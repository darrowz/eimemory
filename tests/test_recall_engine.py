from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, asdict
import inspect
from typing import Any, get_type_hints

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
from eimemory.retrieval.engine import GovernedRecallEngine, RecallCallbacks
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


class HostileDiagnosticsSource:
    name = "hostile"

    def search(self, request: CandidateRequest) -> CandidateBatch:
        secret = "HOSTILE-DIAGNOSTIC-BODY-8675309"
        return CandidateBatch(
            hits=(),
            diagnostics={
                "source_name": secret,
                "candidate_count": "not-an-int",
                "candidate_limit": object(),
                "vector_hits": "also-not-an-int",
                "drops": {"forged": "many"},
                "fallback": True,
                "fallback_reason": secret,
                "nested": {"body": secret * 1000},
            },
        )


class BombHits:
    def __init__(self, hit: CandidateHit) -> None:
        self.hit = hit

    def __iter__(self):
        for _ in range(5000):
            yield self.hit
        raise RuntimeError("hits-read-past-cap")


class BombMap(Mapping):
    def __init__(self, cap: int, message: str) -> None:
        self.cap = cap
        self.message = message

    def __iter__(self):
        for index in range(self.cap):
            yield f"key-{index}"
        raise RuntimeError(self.message)

    def __len__(self) -> int:
        return 10**9

    def __getitem__(self, key: str):
        return {"ok": True}


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


def test_candidate_batch_hard_caps_hits_and_nested_diagnostics() -> None:
    ref = CandidateRef(record_id="r1", scope=ExactScope.from_scope(SCOPE), source_id="alpha")
    hit = CandidateHit(ref=ref, source_rank=1, source_score=1.0)
    batch = CandidateBatch(
        hits=(hit,) * 6000,
        diagnostics={
            "nested": {
                f"key-{index}": {"payload": "x" * 5000}
                for index in range(100)
            }
        },
    )

    assert len(batch.hits) == 5000
    assert len(repr(batch.diagnostic_dict())) < 20_000


def test_candidate_batch_stops_consuming_at_every_hard_boundary() -> None:
    ref = CandidateRef(record_id="r1", scope=ExactScope.from_scope(SCOPE), source_id="alpha")
    hit = CandidateHit(ref=ref, source_rank=1, source_score=1.0)

    hits_batch = CandidateBatch(hits=BombHits(hit))  # type: ignore[arg-type]
    top_batch = CandidateBatch(diagnostics=BombMap(12, "diagnostics-read-past-cap"))  # type: ignore[arg-type]
    nested_batch = CandidateBatch(
        diagnostics={"nested": BombMap(16, "nested-read-past-cap")},
    )

    assert len(hits_batch.hits) == 5000
    assert len(top_batch.diagnostics) == 12
    assert len(top_batch.diagnostic_dict()) == 12
    assert len(nested_batch.diagnostic_dict()["nested"]) == 16


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


def test_governed_engine_uses_an_explicit_typed_callback_contract() -> None:
    init_hints = get_type_hints(GovernedRecallEngine.__init__)
    bind_hints = get_type_hints(GovernedRecallEngine.bind)

    assert init_hints["callbacks"] is not Any
    assert bind_hints["callbacks"] is not Any
    callback_methods = [
        value
        for name, value in RecallCallbacks.__dict__.items()
        if name.startswith("_") and not name.startswith("__") and callable(value)
    ]
    assert callback_methods
    for method in callback_methods:
        signature = inspect.signature(method)
        assert all(
            parameter.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
            for parameter in signature.parameters.values()
        )
        assert signature.return_annotation not in {Any, inspect.Signature.empty}


def test_engine_sanitizes_hostile_provider_diagnostics(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    memory = MemoryAPI(
        store,
        recall_engine=GovernedRecallEngine(store=store, candidate_source=HostileDiagnosticsSource()),
    )

    bundle = memory.recall(query="hostile diagnostics", scope=asdict(SCOPE), limit=2)

    diagnostics = bundle.explanation["engine_diagnostics"]
    assert diagnostics["source_names"] == ["hostile"]
    assert diagnostics["candidate_count"] == 0
    assert diagnostics["fallback_reason"] == "candidate_source_fallback"
    assert "HOSTILE-DIAGNOSTIC-BODY" not in repr(diagnostics)
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


def test_memory_api_rejects_injected_governed_engine_from_another_store(tmp_path) -> None:
    left_store = RuntimeStore(tmp_path / "left")
    right_store = RuntimeStore(tmp_path / "right")
    engine = GovernedRecallEngine(store=left_store, candidate_source=FakeCandidateSource(()))
    try:
        with pytest.raises(ValueError, match="same store"):
            MemoryAPI(right_store, recall_engine=engine)
    finally:
        left_store.close()
        right_store.close()


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


def test_engine_rejects_self_consistent_ref_outside_authorized_query_scopes(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    foreign = store.append(
        _record(
            text="CROSS-SCOPE-SECRET",
            source_id="alpha",
            scope=ScopeRef(tenant_id="foreign", agent_id="agent-x", workspace_id="other", user_id="user-x"),
        )
    )
    source = FakeCandidateSource((_hit(foreign),))
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))

    bundle = memory.recall(
        query="cross scope",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=5,
    )

    assert bundle.items == []
    assert bundle.explanation["engine_diagnostics"]["drops"]["scope_not_allowed"] == 1
    store.close()


def test_engine_enforces_kind_and_source_rank_and_caps_provider_hits(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    rank_one = store.append(_record(text="RANK-ONE", source_id="alpha"))
    rank_two = store.append(_record(text="RANK-TWO", source_id="alpha"))
    injected_rule = RecordEnvelope.create(
        kind="rule",
        title="INJECTED-RULE",
        summary="INJECTED-RULE",
        scope=SCOPE,
        source_id="alpha",
        status="active",
    )
    store.append(injected_rule)
    overflow = tuple(_hit(rank_two, rank=100 + index) for index in range(400))
    source = FakeCandidateSource(
        (
            _hit(rank_two, rank=2),
            _hit(rank_one, rank=1),
            _hit(injected_rule, rank=1),
            *overflow,
        )
    )
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))

    bundle = memory.recall(
        query="rank",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=2,
    )

    assert [item.title for item in bundle.items] == ["RANK-ONE", "RANK-TWO"]
    assert bundle.explanation["engine_diagnostics"]["drops"]["kind_not_allowed"] == 1
    assert bundle.explanation["engine_diagnostics"]["drops"]["provider_over_limit"] > 0
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


def test_explicit_nondefault_source_cannot_emit_default_source_gap(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    rule = RecordEnvelope.create(
        kind="rule",
        title="open source gap",
        summary="open source gap",
        scope=SCOPE,
        source_id="alpha",
        status="active",
        meta={
            "task_type": "recall.source.test",
            "retrieval_policy": {"open_unknown_on_low_confidence": True},
        },
    )
    store.append(rule)
    memory = MemoryAPI(store)

    bundle = memory.recall(
        query="missing alpha knowledge",
        scope=asdict(SCOPE),
        task_context={"task_type": "recall.source.test", "source_ids": ["alpha"]},
        limit=5,
    )

    assert bundle.items == []
    assert bundle.reflections == []
    assert store.list_records(kinds=["unknown"], scope=SCOPE) == []
    store.close()


def test_excluded_source_policy_cannot_control_alpha_recall(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    beta_rule = RecordEnvelope.create(
        kind="rule",
        title="beta policy",
        summary="beta policy",
        scope=SCOPE,
        source_id="beta",
        status="active",
        meta={
            "task_type": "policy.source.test",
            "retrieval_policy": {"route_hint": "task_context_first"},
            "response_policy": {"next_action_hint": "BETA-POLICY-SECRET"},
        },
    )
    store.append(beta_rule)
    alpha = store.append(_record(text="alpha policy-safe answer", source_id="alpha"))
    source = FakeCandidateSource((_hit(alpha),))
    memory = MemoryAPI(store, recall_engine=GovernedRecallEngine(store=store, candidate_source=source))

    bundle = memory.recall(
        query="alpha policy-safe answer",
        scope=asdict(SCOPE),
        task_context={"task_type": "policy.source.test", "source_ids": ["alpha"]},
        limit=1,
    )

    assert bundle.next_action_hint == alpha.title.lower()
    assert bundle.confidence == 0.81
    assert bundle.explanation["active_policy"] == {}
    assert bundle.explanation["policy_suggestions"] == []
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


def test_sqlite_candidate_request_queries_only_its_exact_physical_scope(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    canonical_scope = ScopeRef(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    canonical = store.append(_record(text="exact physical marker canonical", scope=canonical_scope))
    store.append(
        _record(
            text="exact physical marker legacy",
            scope=ScopeRef(tenant_id="default", agent_id="main", workspace_id="repo-x", user_id="darrow"),
        )
    )

    batch = SQLiteCandidateSource(store).search(
        CandidateRequest(
            query="exact physical marker",
            scope=ExactScope.from_scope(canonical_scope),
            kinds=("memory",),
            source_ids=("alpha",),
            limit=10,
            budget=360,
        )
    )

    assert [hit.ref.record_id for hit in batch.hits] == [canonical.record_id]
    assert all(hit.ref.scope == ExactScope.from_scope(canonical_scope) for hit in batch.hits)
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


def test_raw_evidence_rebuilds_same_ref_body_from_authoritative_record(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    alpha = _record(text="AUTHORITATIVE-RAW-BODY", source_id="alpha")
    alpha.record_id = "same_ref_raw"
    store.append(alpha)

    def forged_raw(*args, **kwargs):
        return [
            {
                "record": {
                    "record_id": alpha.record_id,
                    "source_id": "alpha",
                    "scope": asdict(SCOPE),
                    "text": "FORGED-SAME-REF-BODY",
                },
                "final_score": 1.0,
                "boosts": {"forged": 1.0},
            }
        ]

    monkeypatch.setattr("eimemory.retrieval.engine.search_raw_chunks", forged_raw)
    memory = MemoryAPI(store)
    bundle = memory.recall(
        query="authoritative raw body",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "recall_mode": "raw_hybrid"},
        limit=5,
    )

    raw_payload = bundle.explanation["raw_evidence"][0]["record"]
    assert "AUTHORITATIVE-RAW-BODY" in raw_payload["text"]
    assert "FORGED-SAME-REF-BODY" not in repr(bundle.explanation["raw_evidence"])
    assert "forged" not in bundle.explanation["raw_evidence"][0].get("boosts", {})
    store.close()


def test_raw_source_gate_runs_before_llm_reranker(tmp_path) -> None:
    from eimemory.raw.retrieval import search_raw_chunks

    store = RuntimeStore(tmp_path)
    for source_id in ("alpha", "beta"):
        raw = RecordEnvelope.create(
            kind="raw_chunk",
            title=f"pre-llm gate {source_id}",
            summary=f"pre-llm gate {source_id} body",
            content={"raw_text": f"pre-llm gate {source_id} body"},
            scope=SCOPE,
            source="raw",
            source_id=source_id,
        )
        store.append(raw)
    seen_documents: list[str] = []

    def capture_reranker(ranked, **kwargs):
        seen_documents.extend(str(item.get("record", {}).get("text") or "") for item in ranked)
        return [str(item.get("record", {}).get("record_id") or "") for item in ranked]

    results = search_raw_chunks(
        store,
        query="pre-llm gate",
        scope=SCOPE,
        task_context={"llm_reranker": capture_reranker},
        source_ids=["alpha"],
        limit=5,
    )

    assert results
    assert all("beta body" not in document for document in seen_documents)
    assert all(item["record"]["source_id"] == "alpha" for item in results)
    store.close()


def test_raw_api_is_authoritatively_rehydrated_before_llm_without_source_filter(tmp_path, monkeypatch) -> None:
    from eimemory.raw.retrieval import search_raw_chunks

    store = RuntimeStore(tmp_path)
    authoritative = RecordEnvelope.create(
        kind="raw_chunk",
        title="authoritative raw api",
        summary="authoritative raw api",
        content={"raw_text": "AUTHORITATIVE-RAW-API-BODY"},
        scope=SCOPE,
        source="raw",
        source_id="alpha",
    )
    store.append(authoritative)
    monkeypatch.setattr(
        "eimemory.raw.retrieval._raw_api_search",
        lambda *args, **kwargs: [
            {
                "record": {
                    "record_id": authoritative.record_id,
                    "source_id": authoritative.source_id,
                    "scope": asdict(SCOPE),
                    "text": "FORGED-RAW-API-BODY",
                },
                "base_score": 1.0,
            }
        ],
    )
    seen_documents: list[str] = []

    def capture_reranker(ranked, **kwargs):
        seen_documents.extend(str(item.get("record", {}).get("text") or "") for item in ranked)
        return [str(item.get("record", {}).get("record_id") or "") for item in ranked]

    results = search_raw_chunks(
        store,
        query="authoritative raw api",
        scope=SCOPE,
        task_context={"llm_reranker": capture_reranker},
        limit=5,
    )

    assert results
    assert all("FORGED-RAW-API-BODY" not in document for document in seen_documents)
    assert any("AUTHORITATIVE-RAW-API-BODY" in document for document in seen_documents)
    store.close()


def test_raw_recall_deduplicates_by_complete_exact_ref(tmp_path, monkeypatch) -> None:
    user_scope = SCOPE
    global_scope = ScopeRef(
        tenant_id=SCOPE.tenant_id,
        agent_id=SCOPE.agent_id,
        workspace_id=SCOPE.workspace_id,
        user_id="",
    )
    store = RuntimeStore(tmp_path)
    user_record = RecordEnvelope.create(
        kind="raw_chunk",
        title="same id user raw",
        summary="same id user raw",
        content={"raw_text": "same id user raw"},
        scope=user_scope,
        source="raw",
        source_id="alpha",
    )
    global_record = RecordEnvelope.create(
        kind="raw_chunk",
        title="same id global raw",
        summary="same id global raw",
        content={"raw_text": "same id global raw"},
        scope=global_scope,
        source="raw",
        source_id="beta",
    )
    global_record.record_id = user_record.record_id
    store.append(user_record)
    store.append(global_record)
    monkeypatch.setattr(
        "eimemory.raw.retrieval._raw_api_search",
        lambda *args, **kwargs: [{"record": user_record, "base_score": 1.0}],
    )

    bundle = MemoryAPI(store).recall(
        query="same id raw",
        scope=asdict(user_scope),
        task_context={"recall_mode": "raw_hybrid", "source_ids": ["alpha", "beta"]},
        limit=2,
    )

    exact_refs = {
        (
            item["record"]["record_id"],
            item["record"]["source_id"],
            item["record"]["scope"]["user_id"],
        )
        for item in bundle.explanation["raw_evidence"]
    }
    assert exact_refs == {
        (user_record.record_id, "alpha", SCOPE.user_id),
        (global_record.record_id, "beta", ""),
    }
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
    assert bundle.explanation["engine_diagnostics"]["drops"].get("missing_or_corrupt_record", 0) == 1
    store.close()


def test_exact_list_rejects_projection_payload_swap_for_direct_report(tmp_path) -> None:
    import json

    store = RuntimeStore(tmp_path)
    current = RecordEnvelope.create(
        kind="reflection",
        title="CURRENT-REPORT",
        summary="CURRENT-REPORT",
        content={"report_type": "rule_evolution"},
        scope=SCOPE,
        source="eimemory.rule_evolution_loop",
        source_id="alpha",
        meta={"report_type": "rule_evolution"},
    )
    current.record_id = "rule_evolution_projection_swap"
    foreign = RecordEnvelope.create(
        kind="reflection",
        title="FOREIGN-REPORT",
        summary="FOREIGN-REPORT",
        content={"report_type": "rule_evolution"},
        scope=ScopeRef(tenant_id="foreign", agent_id="agent-x", workspace_id="other", user_id="user-x"),
        source="eimemory.rule_evolution_loop",
        source_id="alpha",
        meta={"report_type": "rule_evolution"},
    )
    foreign.record_id = current.record_id
    store.append(current)
    store.append(foreign)
    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = ? WHERE record_id = ? AND tenant_id = ?",
        (json.dumps(foreign.to_dict()), current.record_id, SCOPE.tenant_id),
    )
    store.sqlite.conn.commit()

    assert store.list_by_record_id_exact_scope(current.record_id, scope=SCOPE, source_ids=["alpha"]) == []
    bundle = MemoryAPI(store).recall(
        query=current.record_id,
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "include_report_records": True},
        limit=5,
    )
    assert all(item.title != "FOREIGN-REPORT" for item in [*bundle.items, *bundle.reflections])
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


def test_governed_sqlite_default_preserves_legacy_minimum_candidate_budget(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(text="candidate budget marker", source_id="alpha"))
    bundle = MemoryAPI(store).recall(
        query="candidate budget marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=1,
    )

    assert bundle.explanation["engine_diagnostics"]["candidate_limit"] == 360
    store.close()


def test_sqlite_candidate_search_rejects_projection_payload_swap(tmp_path) -> None:
    import json

    store = RuntimeStore(tmp_path)
    canonical_scope = ScopeRef(
        tenant_id="default",
        agent_id="hongtu",
        workspace_id="embodied",
        user_id="darrow",
    )
    foreign_scope = ScopeRef(
        tenant_id="default",
        agent_id="main",
        workspace_id="repo-x",
        user_id="darrow",
    )
    canonical = _record(text="physical swap marker canonical", scope=canonical_scope, source_id="alpha")
    foreign = _record(text="physical swap marker foreign", scope=foreign_scope, source_id="alpha")
    foreign.record_id = canonical.record_id
    store.append(canonical)
    store.append(foreign)
    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = ? "
        "WHERE record_id = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ? AND source_id = ?",
        (
            json.dumps(foreign.to_dict()),
            canonical.record_id,
            "default",
            "hongtu",
            "embodied",
            "darrow",
            "alpha",
        ),
    )
    store.sqlite.conn.commit()

    batch = SQLiteCandidateSource(store).search(
        CandidateRequest(
            query="physical swap marker canonical",
            scope=ExactScope.from_scope(canonical_scope),
            kinds=("memory",),
            source_ids=("alpha",),
            limit=10,
            budget=360,
        )
    )

    assert batch.hits == ()
    store.close()


def test_exact_scope_fanout_preserves_joint_sqlite_top_one_ranking(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    user_scope = ScopeRef(tenant_id="tenant-z", agent_id="agent-z", workspace_id="workspace-z", user_id="user-z")
    global_scope = ScopeRef(tenant_id="tenant-z", agent_id="agent-z", workspace_id="workspace-z", user_id="")
    user = _record(text="marker peripheral detail", scope=user_scope, source_id="alpha")
    user.meta["quality"]["salience_score"] = 0.1
    global_exact = _record(text="marker exact target", scope=global_scope, source_id="alpha")
    global_exact.meta["quality"]["salience_score"] = 0.99
    store.append(user)
    store.append(global_exact)
    legacy, _report = store.search_with_diagnostics(
        query="marker exact target",
        kinds=["memory", "claim_card", "knowledge_page"],
        scope=user_scope,
        limit=3,
        recall_filters={
            "scoring_profile": "balanced",
            "blocked_recall_lanes": ["run_log", "audit_record", "incident_report", "evolution_artifact"],
        },
        source_ids=["alpha"],
    )

    bundle = MemoryAPI(store).recall(
        query="marker exact target",
        scope=asdict(user_scope),
        task_context={"source_ids": ["alpha"], "recall_profile": "balanced"},
        limit=1,
    )

    assert legacy[0].record_id == global_exact.record_id
    assert [item.record_id for item in bundle.items] == [legacy[0].record_id]
    store.close()


def test_default_source_allowlist_preserves_default_policy_search(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.upsert_intent_pattern(
        {
            "pattern": "policy default marker",
            "default_event_type": "policy_default_event",
            "interpreted_intent": "default policy",
            "execution_policy": ["default action"],
            "success_criteria": "default success",
        },
        scope=SCOPE,
    )
    memory = MemoryAPI(store)

    unrestricted = memory.recall(query="policy default marker", scope=asdict(SCOPE), limit=1)
    explicit_default = memory.recall(
        query="policy default marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["default"]},
        limit=1,
    )
    explicit_alpha = memory.recall(
        query="policy default marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=1,
    )

    assert unrestricted.explanation["policy_suggestions"]
    assert explicit_default.explanation["policy_suggestions"] == unrestricted.explanation["policy_suggestions"]
    assert explicit_default.explanation["matched_event_type"] == "policy_default_event"
    assert explicit_alpha.explanation["policy_suggestions"] == []
    store.close()
