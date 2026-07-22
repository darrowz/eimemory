from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, asdict
import inspect
import json
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
from eimemory.storage.jsonl import canonical_payload_json, payload_digest
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


def test_effective_identity_binds_candidate_budget_policy(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    engine = GovernedRecallEngine(store=store, candidate_source=SQLiteCandidateSource(store))

    original = engine.effective_identity()
    assert original["policy_version"] == "governed-recall.v2"
    assert original["candidate_budget_policy"] == {
        "minimum": 48,
        "multiplier": 3,
        "max_query_scope_refs": 64,
    }

    engine._candidate_budget_multiplier = 4
    changed = engine.effective_identity()
    assert changed["identity_digest"] != original["identity_digest"]
    store.close()


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


def test_recall_hydrates_legacy_knowledge_source_from_authoritative_projection(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    target = RecordEnvelope.create(
        kind="knowledge_page",
        title="Legacy source projection",
        summary="A migrated knowledge page must remain searchable in its authoritative source partition.",
        scope=SCOPE,
        source="eimemory.knowledge.synthesis",
        source_id="alpha",
        content={"source_ids": ["alpha", "beta"]},
    )
    store.append(target)

    row = store.sqlite.conn.execute(
        "SELECT storage_key, payload_json FROM records WHERE record_id = ? AND source_id = ?",
        (target.record_id, "alpha"),
    ).fetchone()
    legacy_payload = json.loads(str(row["payload_json"]))
    legacy_payload["source_id"] = "default"
    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = ?, payload_digest = ? WHERE storage_key = ?",
        (
            canonical_payload_json(legacy_payload),
            payload_digest(legacy_payload),
            str(row["storage_key"]),
        ),
    )
    store.sqlite.conn.commit()

    records, report = store.search_with_diagnostics(
        query="migrated knowledge authoritative source partition",
        kinds=["knowledge_page"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True},
        source_ids=["alpha"],
    )

    assert [record.record_id for record in records] == [target.record_id]
    assert report["blocked_counts"].get("projection_payload_mismatch", 0) == 0
    store.close()


def test_recall_rejects_tampered_inline_payload_before_legacy_source_overlay(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    target = RecordEnvelope.create(
        kind="knowledge_page",
        title="Trusted projection title",
        summary="Trusted projection summary",
        scope=SCOPE,
        source="eimemory.knowledge.synthesis",
        source_id="alpha",
        content={"source_ids": ["alpha"]},
    )
    store.append(target)

    row = store.sqlite.conn.execute(
        "SELECT storage_key, payload_json FROM records WHERE record_id = ? AND source_id = ?",
        (target.record_id, "alpha"),
    ).fetchone()
    tampered = json.loads(str(row["payload_json"]))
    tampered["source_id"] = "default"
    tampered["title"] = "TAMPERED"
    tampered["summary"] = "TAMPERED"
    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = ? WHERE storage_key = ?",
        (canonical_payload_json(tampered), str(row["storage_key"])),
    )
    store.sqlite.conn.commit()

    records, report = store.search_with_diagnostics(
        query="Trusted projection",
        kinds=["knowledge_page"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True},
        source_ids=["alpha"],
    )

    assert records == []
    assert report["blocked_counts"]["corrupt_record"] == 1
    assert store.get_by_exact_ref(
        target.record_id,
        scope=SCOPE,
        source_id="alpha",
    ) is None
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


def test_governed_sqlite_default_uses_bounded_candidate_budget(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(text="candidate budget marker", source_id="alpha"))
    bundle = MemoryAPI(store).recall(
        query="candidate budget marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=1,
    )

    assert bundle.explanation["engine_diagnostics"]["candidate_limit"] == 48
    store.close()


def test_exact_empty_scope_short_circuits_before_fts_and_anchor(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(text="record exists only in another scope", source_id="alpha"))
    empty_scope = ScopeRef(
        tenant_id="tenant-a",
        agent_id="missing-agent",
        workspace_id="missing-workspace",
        user_id="missing-user",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("empty exact scopes must not execute FTS or anchor scans")

    monkeypatch.setattr(store.sqlite, "_collect_fts_candidates", forbidden)
    monkeypatch.setattr(store.sqlite, "_collect_anchor_candidates", forbidden)

    records, report = store.search_with_diagnostics(
        query="record exists",
        kinds=["memory"],
        scope=empty_scope,
        limit=5,
        recall_filters={"_exact_scope": True},
        source_ids=["alpha"],
    )

    assert records == []
    assert report["candidate_count"] == 0
    assert report["candidate_short_circuit"] == "empty_exact_scope"
    store.close()


def test_fts_hit_keeps_anchor_scan_within_reserved_budget(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    targets = [
        store.append(_record(text=f"indexed deployment marker {index}", source_id="alpha"))
        for index in range(8)
    ]

    anchor_limits: list[int] = []
    original_anchor = store.sqlite._collect_anchor_candidates

    def recording_anchor(*args, **kwargs):
        anchor_limits.append(int(kwargs["limit"]))
        return original_anchor(*args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("lane fallback must not run after lexical candidates")

    monkeypatch.setattr(store.sqlite, "_collect_anchor_candidates", recording_anchor)
    monkeypatch.setattr(store.sqlite, "_collect_lane_seed_candidates", forbidden)

    records, report = store.search_with_diagnostics(
        query="indexed deployment marker",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True, "intent_name": "project_delivery"},
        source_ids=["alpha"],
    )

    assert {record.record_id for record in records}.issubset({target.record_id for target in targets})
    assert report["candidate_sources"]["fts"] >= 5
    assert anchor_limits == [16]
    store.close()


def test_sparse_fts_hit_falls_back_to_anchor_before_declaring_no_match(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(text="验证", source_id="alpha"))
    strong = store.append(_record(text="部署完成后必须验证生产健康状态", source_id="alpha"))

    records, report = store.search_with_diagnostics(
        query="验证生产健康",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True},
        source_ids=["alpha"],
    )

    assert records[0].record_id == strong.record_id
    assert report["candidate_sources"]["fts"] >= 1
    assert report["candidate_sources"]["anchor"] >= 1
    store.close()


def test_dense_weak_chinese_fts_pool_cannot_hide_strong_anchor_match(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    for index in range(8):
        weak = _record(text=f"验证生 {index}", source_id="alpha")
        weak.meta["quality"]["salience_score"] = 0.05
        store.append(weak)
    strong = _record(text="部署完成后必须验证生产健康状态", source_id="alpha")
    strong.meta["quality"]["salience_score"] = 1.0
    store.append(strong)

    records, report = store.search_with_diagnostics(
        query="验证生产健康",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True, "candidate_limit": 64},
        source_ids=["alpha"],
    )

    assert records[0].record_id == strong.record_id
    assert report["candidate_sources"]["fts"] >= 8
    assert report["candidate_sources"]["anchor"] >= 1
    store.close()


def test_dense_weak_ascii_fts_pool_cannot_hide_substring_anchor_match(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    for index in range(8):
        weak = _record(text=f"alphabeta weak {index}", source_id="alpha")
        weak.meta["quality"]["salience_score"] = 0.05
        store.append(weak)
    strong = _record(text="xalphabetay authoritative durable target", source_id="alpha")
    strong.meta["quality"]["salience_score"] = 1.0
    store.append(strong)

    records, report = store.search_with_diagnostics(
        query="alphabeta",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True, "candidate_limit": 64},
        source_ids=["alpha"],
    )

    assert strong.record_id in {record.record_id for record in records}
    strong_report = next(item for item in report["scored_items"] if item["record_id"] == strong.record_id)
    assert strong_report["selection_reserve"] == "high_quality_anchor_only"
    assert report["candidate_sources"]["fts"] >= 8
    assert report["candidate_sources"]["anchor"] >= 1

    bundle = MemoryAPI(store).recall(
        query="alphabeta",
        scope=asdict(SCOPE),
        task_context={"exact_scope_only": True, "source_ids": ["alpha"]},
        limit=5,
    )
    assert strong.record_id in {record.record_id for record in bundle.items}
    assert bundle.explanation["engine_diagnostics"]["drops"]["anchor_reserve_swap"] == 1
    store.close()


def test_anchor_reserve_does_not_replace_exact_token_hits_with_short_substring_noise(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    exact = []
    for index in range(5):
        record = _record(text=f"cat fact {index}", source_id="alpha")
        record.meta["quality"]["salience_score"] = 0.9
        exact.append(store.append(record))
    noise = _record(text="catastrophe", source_id="alpha")
    noise.meta["quality"]["salience_score"] = 1.0
    store.append(noise)

    records, _report = store.search_with_diagnostics(
        query="cat",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True, "candidate_limit": 64},
        source_ids=["alpha"],
    )

    assert {record.record_id for record in records} == {record.record_id for record in exact}
    assert noise.record_id not in {record.record_id for record in records}

    bundle = MemoryAPI(store).recall(
        query="cat",
        scope=asdict(SCOPE),
        task_context={"exact_scope_only": True, "source_ids": ["alpha"]},
        limit=5,
    )
    assert {record.record_id for record in bundle.items} == {record.record_id for record in exact}
    store.close()


def test_exact_scope_with_partial_recall_index_uses_bounded_legacy_fallback(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    scope_a = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="partial-a", user_id="user-a")
    scope_b = ScopeRef(tenant_id="tenant-a", agent_id="agent-b", workspace_id="partial-b", user_id="user-b")
    target = store.append(_record(text="partial migration target", source_id="alpha", scope=scope_a))
    store.append(_record(text="indexed other scope", source_id="alpha", scope=scope_b))
    storage_key = store.sqlite.conn.execute(
        "SELECT storage_key FROM records WHERE record_id = ? AND workspace_id = ?",
        (target.record_id, scope_a.workspace_id),
    ).fetchone()[0]
    store.sqlite.conn.execute("DELETE FROM recall_index WHERE storage_key = ?", (storage_key,))
    store.sqlite.conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id = ?",
        ("recall.identity_index.v1",),
    )
    if store.sqlite._has_fts_table():
        store.sqlite.conn.execute("DELETE FROM recall_index_fts WHERE storage_key = ?", (storage_key,))
    store.sqlite.conn.commit()

    records, report = store.search_with_diagnostics(
        query="partial migration target",
        kinds=["memory"],
        scope=scope_a,
        limit=5,
        recall_filters={"_exact_scope": True, "candidate_limit": 64},
        source_ids=["alpha"],
    )

    assert [record.record_id for record in records] == [target.record_id]
    assert report["candidate_fallback"] == "legacy_scan"
    assert report["candidate_count"] <= 64
    store.close()


def test_candidate_collectors_share_the_declared_budget(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(text="bounded collector budget", source_id="alpha"))
    limits: list[int] = []
    original_fts = store.sqlite._collect_fts_candidates
    original_anchor = store.sqlite._collect_anchor_candidates

    def recording_fts(*args, **kwargs):
        limits.append(int(kwargs["limit"]))
        return original_fts(*args, **kwargs)

    def recording_anchor(*args, **kwargs):
        limits.append(int(kwargs["limit"]))
        return original_anchor(*args, **kwargs)

    monkeypatch.setattr(store.sqlite, "_collect_fts_candidates", recording_fts)
    monkeypatch.setattr(store.sqlite, "_collect_anchor_candidates", recording_anchor)

    _records, report = store.search_with_diagnostics(
        query="bounded collector budget",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True, "candidate_limit": 64},
        source_ids=["alpha"],
    )

    assert limits == [48, 16]
    assert sum(limits) == 64
    assert report["candidate_count"] <= 64
    store.close()


def test_bounded_fts_pool_reserves_high_quality_matches_for_reranking(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    target = RecordEnvelope.create(
        kind="memory",
        title="alpha authoritative preference",
        summary="alpha " + ("durable operator preference " * 160),
        content={"text": "alpha " + ("durable operator preference " * 160)},
        scope=SCOPE,
        source="operator.preference",
        source_id="alpha",
        meta={
            "memory_type": "preference",
            "quality": {"salience_score": 1.0, "importance": 1.0, "capture_decision": "accept"},
        },
    )
    store.append(target)
    for index in range(100):
        store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"alpha distractor {index}",
                summary="alpha",
                content={"text": "alpha"},
                scope=SCOPE,
                source="test.noise",
                source_id="alpha",
                meta={
                    "memory_type": "fact",
                    "quality": {"salience_score": 0.0, "importance": 0.0, "capture_decision": "accept"},
                },
            )
        )

    batch = SQLiteCandidateSource(store).search(
        CandidateRequest.create(
            query="alpha",
            scope=SCOPE,
            kinds=("memory",),
            source_ids=("alpha",),
            limit=64,
            budget=64,
            recall_filters={"candidate_limit": 64},
        )
    )

    assert batch.hits[0].ref.record_id == target.record_id
    assert any(hit.ref.record_id == target.record_id for hit in batch.hits)
    assert batch.diagnostic_dict()["candidate_count"] <= 64

    bundle = MemoryAPI(store).recall(
        query="alpha",
        scope=asdict(SCOPE),
        task_context={"exact_scope_only": True, "source_ids": ["alpha"]},
        limit=5,
    )
    assert target.record_id in {record.record_id for record in bundle.items}
    store.close()


def test_filtered_fts_pool_retries_anchor_fallback(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    for _index in range(8):
        store.append(_record(text="alphabeta", source_id="alpha", status="inactive"))
    strong = store.append(_record(text="xalphabetay", source_id="alpha"))

    records, report = store.search_with_diagnostics(
        query="alphabeta",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True},
        source_ids=["alpha"],
    )

    assert records[0].record_id == strong.record_id
    assert report["candidate_sources"]["anchor"] >= 1
    store.close()


def test_exact_identity_candidate_bounds_hybrid_search(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    target = store.append(_record(text="UNIQUE EXACT IDENTITY TITLE", source_id="alpha"))
    calls: list[dict] = []
    original = store.search_with_diagnostics

    def recording_search(*args, **kwargs):
        calls.append(dict(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "search_with_diagnostics", recording_search)

    batch = SQLiteCandidateSource(store).search(
        CandidateRequest(
            query=target.title,
            scope=ExactScope.from_scope(SCOPE),
            kinds=("memory",),
            source_ids=("alpha",),
            limit=5,
            budget=360,
        )
    )

    assert [hit.ref.record_id for hit in batch.hits] == [target.record_id]
    assert calls[0]["limit"] <= 32
    assert calls[0]["recall_filters"]["candidate_limit"] <= 96
    assert batch.diagnostic_dict()["retrieval_mode"] == "identity_hybrid"
    store.close()


def test_single_result_identity_candidate_skips_hybrid_search(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    target = store.append(_record(text="EXACT SCOPE IDENTITY TITLE", source_id="alpha"))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an exact-scope identity hit must not run fuzzy retrieval")

    monkeypatch.setattr(store, "search_with_diagnostics", forbidden)

    batch = SQLiteCandidateSource(store).search(
        CandidateRequest.create(
            query=target.title,
            scope=SCOPE,
            kinds=("memory",),
            source_ids=("alpha",),
            limit=1,
            budget=360,
            recall_filters={"_result_limit": 1},
        )
    )

    assert [hit.ref.record_id for hit in batch.hits] == [target.record_id]
    assert batch.diagnostic_dict()["retrieval_mode"] == "identity_index"
    store.close()


def test_fts_zero_hit_keeps_anchor_fallback_for_chinese_substring(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    target = store.append(_record(text="部署完成后必须验证生产健康状态", source_id="alpha"))
    monkeypatch.setattr(store.sqlite, "_collect_fts_candidates", lambda *_args, **_kwargs: None)

    records, report = store.search_with_diagnostics(
        query="验证生产健康",
        kinds=["memory"],
        scope=SCOPE,
        limit=5,
        recall_filters={"_exact_scope": True},
        source_ids=["alpha"],
    )

    assert [record.record_id for record in records] == [target.record_id]
    assert report["candidate_sources"] == {"anchor": 1}
    store.close()


def test_default_recall_does_not_issue_operational_diagnostic_requeries(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(text="ordinary recall marker", source_id="alpha"))
    limits: list[int] = []
    original = store.sqlite.search_with_diagnostics

    def recording_search(*args, **kwargs):
        limits.append(int(kwargs.get("limit") or 0))
        return original(*args, **kwargs)

    monkeypatch.setattr(store.sqlite, "search_with_diagnostics", recording_search)

    bundle = MemoryAPI(store).recall(
        query="ordinary recall marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=1,
    )

    assert [item.title for item in bundle.items] == ["ordinary recall marker"]
    assert 24 not in limits
    assert 3 not in limits
    store.close()


def test_explicit_recall_diagnostics_still_runs_bounded_operational_probe(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    store.append(_record(text="explicit probe marker", source_id="alpha"))
    limits: list[int] = []
    original = store.sqlite.search_with_diagnostics

    def recording_search(*args, **kwargs):
        limits.append(int(kwargs.get("limit") or 0))
        return original(*args, **kwargs)

    monkeypatch.setattr(store.sqlite, "search_with_diagnostics", recording_search)

    MemoryAPI(store).recall(
        query="explicit probe marker",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"], "recall_diagnostics": True},
        limit=1,
    )

    assert 24 in limits
    store.close()


def test_authoritative_online_gate_blocks_outcome_candidate_but_report_can_request_it(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    outcome = RecordEnvelope.create(
        kind="memory",
        title="OpenClaw agent outcome",
        summary="An agent outcome summary must remain operational evidence.",
        content={"text": "An agent outcome summary must remain operational evidence.", "memory_type": "fact"},
        scope=SCOPE,
        source="openclaw.agent_end",
        source_id="alpha",
        meta={"memory_type": "fact"},
    )
    store.append(outcome)
    memory = MemoryAPI(
        store,
        recall_engine=GovernedRecallEngine(store=store, candidate_source=FakeCandidateSource((_hit(outcome),))),
    )

    ordinary = memory.recall(
        query="terminal agent result",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=3,
    )
    report = memory.recall(
        query="governance report terminal agent result",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=3,
    )

    assert ordinary.items == []
    assert ordinary.explanation["online_recall_gate"]["blocked_counts"]["agent_outcome"] == 1
    assert [item.record_id for item in report.items] == [outcome.record_id]
    store.close()


def test_user_alias_fanout_is_hard_bounded(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    source = FakeCandidateSource(())
    canonical_scope = ScopeRef(
        tenant_id="default",
        agent_id="hongtu",
        workspace_id="embodied",
        user_id="darrow",
    )
    memory = MemoryAPI(
        store,
        recall_engine=GovernedRecallEngine(store=store, candidate_source=source),
    )

    bundle = memory.recall(
        query="bounded alias fanout",
        scope=asdict(canonical_scope),
        task_context={
            "source_ids": ["alpha"],
            "user_aliases": [f"alias-{index}" for index in range(1000)],
        },
        limit=1,
    )

    assert len(source.requests) <= 128
    assert bundle.explanation["engine_diagnostics"]["drops"]["query_scope_limit"] >= 1
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


def test_sqlite_candidate_search_rejects_cross_scope_recall_index_projection(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    exact_scope = ScopeRef(
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
    store.append(_record(text="unrelated exact record", scope=exact_scope, source_id="alpha"))
    foreign = store.append(
        _record(text="INDEX-CROSS-SCOPE-MARKER", scope=foreign_scope, source_id="alpha")
    )
    storage_key = str(
        store.sqlite.conn.execute(
            "SELECT storage_key FROM records WHERE record_id = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ?",
            (foreign.record_id, foreign_scope.tenant_id, foreign_scope.agent_id, foreign_scope.workspace_id),
        ).fetchone()[0]
    )
    store.sqlite.conn.execute(
        "UPDATE recall_index SET tenant_id = ?, agent_id = ?, workspace_id = ?, user_id = ? WHERE storage_key = ?",
        (exact_scope.tenant_id, exact_scope.agent_id, exact_scope.workspace_id, exact_scope.user_id, storage_key),
    )
    store.sqlite.conn.commit()

    batch = SQLiteCandidateSource(store).search(
        CandidateRequest(
            query="INDEX-CROSS-SCOPE-MARKER",
            scope=ExactScope.from_scope(exact_scope),
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


def test_hongtu_logical_scope_order_precedes_cross_alias_score(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    main_scope = ScopeRef(
        tenant_id="default",
        agent_id="main",
        workspace_id="repo-x",
        user_id="darrow",
    )
    canonical_scope = ScopeRef(
        tenant_id="default",
        agent_id="hongtu",
        workspace_id="embodied",
        user_id="darrow",
    )
    main = _record(text="marker peripheral detail", scope=main_scope, source_id="alpha")
    main.title = "MAIN-PARTIAL"
    main.meta["quality"]["salience_score"] = 0.1
    canonical = _record(text="marker exact target", scope=canonical_scope, source_id="alpha")
    canonical.title = "CANONICAL-EXACT"
    canonical.meta["quality"]["salience_score"] = 0.99
    store.append(main)
    store.append(canonical)

    bundle = MemoryAPI(store).recall(
        query="marker exact target",
        scope=asdict(main_scope),
        task_context={"source_ids": ["alpha"], "recall_profile": "balanced"},
        limit=1,
    )

    assert [item.record_id for item in bundle.items] == [main.record_id]
    store.close()


def test_canonical_hongtu_group_jointly_scores_legacy_aliases(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    canonical_scope = ScopeRef(
        tenant_id="default",
        agent_id="hongtu",
        workspace_id="embodied",
        user_id="darrow",
    )
    legacy_scope = ScopeRef(
        tenant_id="default",
        agent_id="main",
        workspace_id="repo-x",
        user_id="darrow",
    )
    canonical = _record(text="marker peripheral detail", scope=canonical_scope, source_id="alpha")
    canonical.title = "CANONICAL-PARTIAL"
    canonical.meta["quality"]["salience_score"] = 0.1
    legacy = _record(text="marker exact target", scope=legacy_scope, source_id="alpha")
    legacy.title = "LEGACY-EXACT"
    legacy.meta["quality"]["salience_score"] = 0.99
    store.append(canonical)
    store.append(legacy)
    legacy_joint, _report = store.search_with_diagnostics(
        query="marker exact target",
        kinds=["memory", "claim_card", "knowledge_page"],
        scope=canonical_scope,
        limit=3,
        recall_filters={
            "scoring_profile": "balanced",
            "blocked_recall_lanes": ["run_log", "audit_record", "incident_report", "evolution_artifact"],
        },
        source_ids=["alpha"],
    )

    bundle = MemoryAPI(store).recall(
        query="marker exact target",
        scope=asdict(canonical_scope),
        task_context={"source_ids": ["alpha"], "recall_profile": "balanced"},
        limit=1,
    )

    assert legacy_joint[0].record_id == legacy.record_id
    assert [item.record_id for item in bundle.items] == [legacy_joint[0].record_id]
    store.close()


def test_inactive_projection_cannot_be_revived_by_active_payload(tmp_path) -> None:
    import json

    store = RuntimeStore(tmp_path)
    inactive = store.append(
        _record(text="INACTIVE-PAYLOAD-REVIVAL-MARKER", source_id="alpha", status="inactive")
    )
    forged_payload = inactive.to_dict()
    forged_payload["status"] = "active"
    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = ? WHERE record_id = ? AND source_id = ?",
        (json.dumps(forged_payload), inactive.record_id, inactive.source_id),
    )
    store.sqlite.conn.commit()

    exact = store.get_by_exact_ref(
        inactive.record_id,
        scope=SCOPE,
        source_id="alpha",
    )
    batch = SQLiteCandidateSource(store).search(
        CandidateRequest(
            query="INACTIVE-PAYLOAD-REVIVAL-MARKER",
            scope=ExactScope.from_scope(SCOPE),
            kinds=("memory",),
            source_ids=("alpha",),
            limit=5,
            budget=360,
        )
    )
    bundle = MemoryAPI(store).recall(
        query="INACTIVE-PAYLOAD-REVIVAL-MARKER",
        scope=asdict(SCOPE),
        task_context={"source_ids": ["alpha"]},
        limit=5,
    )

    assert exact is None
    assert batch.hits == ()
    assert bundle.items == []
    store.close()


def test_physical_kind_projection_cannot_be_disguised_by_payload(tmp_path) -> None:
    import json

    store = RuntimeStore(tmp_path)
    rule = RecordEnvelope.create(
        kind="rule",
        title="PHYSICAL-RULE-KIND",
        summary="PHYSICAL-RULE-KIND",
        scope=SCOPE,
        source_id="alpha",
        status="active",
    )
    store.append(rule)
    forged_payload = rule.to_dict()
    forged_payload["kind"] = "memory"
    store.sqlite.conn.execute(
        "UPDATE records SET payload_json = ? WHERE record_id = ? AND source_id = ?",
        (json.dumps(forged_payload), rule.record_id, rule.source_id),
    )
    store.sqlite.conn.commit()

    assert store.get_by_exact_ref(rule.record_id, scope=SCOPE, source_id="alpha") is None
    assert store.list_by_record_id_exact_scope(rule.record_id, scope=SCOPE, source_ids=["alpha"]) == []
    store.close()
